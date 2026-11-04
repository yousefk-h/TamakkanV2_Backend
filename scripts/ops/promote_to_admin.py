"""One-off: promote a single existing user (by email) to `role: "administrator"` (Center Admin) in the REAL
Tamakkan production database.

Follows the exact same safety model as `migrate_teacher_roles.py` (read that script's docstring for the full
rationale) - this one is kept separate rather than added as a subcommand there so the carefully fail-closed
administrator->teacher migration logic in that script is never touched by this unrelated teacher->administrator
promotion.

- Independent of `app.core.config`/`app.core.database` - connects only via `pymongo`, only to whatever
  `--uri`/`TAMAKKAN_PROD_MONGODB_URI` you give it. Never triggers Beanie index sync against prod.
- The connection string is never printed, logged, or written to a file.
- Every run needs --db, verified against the live connection's actual resolved database name.
- `inspect` (default-safe, read-only) prints the target account's current role/status. It cannot write.
- `promote` defaults to --dry-run. `--apply` additionally requires --confirm-db (must repeat --db exactly) and
  --backup-ref (where you already put a backup - this script does not take it for you).
- The only field ever written is `role` (plus `updated_at`), and only for the single user id resolved from
  --email. The write is compare-and-set: `{"_id": id, "role": <role at inspect time>}` -> `{"role": "administrator"}`,
  so it only ever flips the role this run itself observed, and is idempotent (re-running after a successful
  apply is a no-op, not an error).
- Refuses outright if the account is already role=administrator or role=root (nothing to promote) or does not
  exist.

USAGE
-----
    export TAMAKKAN_PROD_MONGODB_URI="<the uri - avoid putting this in shell history>"

    # Read-only: see the account's current role/status.
    python scripts/ops/promote_to_admin.py inspect --db tamakkan --email rand@tamakkan.com

    # Dry run of the promotion (no writes, ever, regardless of flags).
    python scripts/ops/promote_to_admin.py promote --db tamakkan --email rand@tamakkan.com

    # Apply (only after reviewing the dry-run output above and taking a backup).
    python scripts/ops/promote_to_admin.py promote --db tamakkan --apply \\
        --confirm-db tamakkan --backup-ref "mongodump 2026-10-20 -> /root/tamakkan-recovery/pre-promote-rand-20261020" \\
        --email rand@tamakkan.com
"""

from __future__ import annotations

import argparse
import asyncio
import os
import sys
from datetime import datetime, timezone
from typing import Optional

from pymongo import AsyncMongoClient

USERS_COLLECTION = "users"

# Mirrors migrate_teacher_roles.py's tripwire - not the real safety boundary (--confirm-db is), just a guard
# against a copy-pasted local --uri silently "succeeding" against a throwaway local db.
_SUSPICIOUS_DB_NAME_MARKERS = ("test", "local", "integration", "_dev", "staging")

# Fields ever read from the user document - no password_hash, tokens, or verification hashes.
USER_PROJECTION = {
    "_id": 1,
    "full_name": 1,
    "email": 1,
    "role": 1,
    "is_active": 1,
    "created_at": 1,
    "created_by_id": 1,
}

PROMOTABLE_ROLES = {"student", "teacher"}


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
    await client.admin.command("ping")
    actual_name = db.name
    if actual_name != db_name:
        await client.close()
        raise SystemExit(f"Connected, but the resolved database name ('{actual_name}') does not match --db ('{db_name}').")
    return client, db


def _fmt_date(value: Optional[datetime]) -> str:
    return value.date().isoformat() if value else "unknown"


def _print_account(doc: dict) -> None:
    print(f"  full_name:     {doc.get('full_name', '')}")
    print(f"  email:         {doc.get('email', '')}")
    print(f"  id:            {doc['_id']}")
    print(f"  role:          {doc.get('role', '')}")
    print(f"  is_active:     {doc.get('is_active', True)}")
    print(f"  created_at:    {_fmt_date(doc.get('created_at'))}")
    print(f"  created_by_id: {doc.get('created_by_id')}")


async def cmd_inspect(args: argparse.Namespace) -> int:
    uri = _uri_from_args(args)
    _check_db_name_sanity(args.db)
    client, db = await _connect(uri, args.db)
    try:
        doc = await db[USERS_COLLECTION].find_one({"email": args.email.strip().lower()}, USER_PROJECTION)
        if doc is None:
            print(f"No user found with email '{args.email}'.")
            return 1
        print(f"DATABASE: {args.db}\n")
        _print_account(doc)
        return 0
    finally:
        await client.close()


async def cmd_promote(args: argparse.Namespace) -> int:
    uri = _uri_from_args(args)
    _check_db_name_sanity(args.db)
    email = args.email.strip().lower()

    client, db = await _connect(uri, args.db)
    try:
        doc = await db[USERS_COLLECTION].find_one({"email": email}, USER_PROJECTION)
        if doc is None:
            raise SystemExit(f"No user found with email '{email}'.")

        current_role = doc.get("role", "")
        print(f"DATABASE:\n  {args.db}\n")
        print("CURRENT ACCOUNT:")
        _print_account(doc)

        if current_role == "administrator":
            print("\nNo-op: already role=administrator.")
            return 0
        if current_role not in PROMOTABLE_ROLES:
            raise SystemExit(f"Refusing: role='{current_role}' is not one of {sorted(PROMOTABLE_ROLES)} - this script only promotes student/teacher accounts.")

        print(f"\nPROPOSED CHANGE:\n  role: {current_role} -> administrator")

        if not args.apply:
            print("\nDRY RUN - no database writes were made. Re-run with --apply (plus --confirm-db and --backup-ref) to write.")
            return 0

        if args.confirm_db != args.db:
            raise SystemExit("--apply requires --confirm-db to exactly repeat --db, as a last explicit confirmation.")
        if not args.backup_ref:
            raise SystemExit("--apply requires --backup-ref: where the pre-promotion backup is stored. Take it first.")

        print(f"\nBacked up at: {args.backup_ref}")
        print("APPLYING...")
        now = datetime.now(timezone.utc)
        result = await db[USERS_COLLECTION].update_one(
            {"_id": doc["_id"], "role": current_role},  # compare-and-set: only flips the role this run observed
            {"$set": {"role": "administrator", "updated_at": now}},
        )
        print(f"modified {result.modified_count} document(s)")

        after = await db[USERS_COLLECTION].find_one({"_id": doc["_id"]}, USER_PROJECTION)
        ok = after is not None and after.get("role") == "administrator"
        print(f"\nPOST-WRITE VERIFICATION: role=administrator -> {ok}")
        if not ok:
            print("WARNING: promotion did not take effect as expected - check manually.")
            return 1
        return 0
    finally:
        await client.close()


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--uri", default=None, help="Mongo connection URI. Prefer TAMAKKAN_PROD_MONGODB_URI instead - never put this in shell history.")
    sub = p.add_subparsers(dest="command", required=True)

    i = sub.add_parser("inspect", help="Read-only: print the target account's current role/status.")
    i.add_argument("--db", required=True, help="Exact database name - verified against the live connection.")
    i.add_argument("--email", required=True, help="Email of the account to inspect.")
    i.set_defaults(func=cmd_inspect)

    pr = sub.add_parser("promote", help="Dry-run by default; --apply performs the real, idempotent role promotion to administrator.")
    pr.add_argument("--db", required=True)
    pr.add_argument("--email", required=True, help="Email of the account to promote to administrator.")
    pr.add_argument("--apply", action="store_true", help="Actually write. Omit for a dry run (the default).")
    pr.add_argument("--confirm-db", default=None, help="Required with --apply: must repeat --db exactly.")
    pr.add_argument("--backup-ref", default=None, help="Required with --apply: where the pre-promotion backup is stored.")
    pr.set_defaults(func=cmd_promote)

    return p


def main() -> int:
    args = build_parser().parse_args()
    return asyncio.run(args.func(args))


if __name__ == "__main__":
    sys.exit(main())
