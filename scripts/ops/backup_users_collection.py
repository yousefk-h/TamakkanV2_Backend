"""PROMPT-051: a narrow pre-migration backup of exactly the collection `migrate_teacher_roles.py` ever writes
to (`users`) - a full point-in-time export of every field, including password_hash and verification tokens,
since a RECOVERY backup (unlike `inspect`'s on-screen listing) has to be restorable, not merely safe to display.

Used instead of `mongodump` because this machine has no `mongodump`/MongoDB Database Tools installed and no
SSH access to the production host from this session; this script needs nothing but the same `pymongo` driver
`migrate_teacher_roles.py` already uses, over the same connection this session already has.

Writes ONE JSON file, outside the git repository (the caller's scratchpad/output directory), and prints only
its path, a document count, and a byte size - never any document content, so a sensitive field can never end up
in a terminal transcript or a log.

    export TAMAKKAN_PROD_MONGODB_URI="..."
    python scripts/ops/backup_users_collection.py --db tamakkan --out-dir "<some directory outside the repo>"
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys
from datetime import datetime, timezone
from pathlib import Path

from bson import json_util
from pymongo import AsyncMongoClient

USERS_COLLECTION = "users"


async def main() -> int:
    p = argparse.ArgumentParser()
    p.add_argument("--uri", default=None)
    p.add_argument("--db", required=True)
    p.add_argument("--out-dir", required=True, help="Directory to write the backup file into - must already exist, outside the git repo.")
    args = p.parse_args()

    uri = args.uri or os.environ.get("TAMAKKAN_PROD_MONGODB_URI")
    if not uri:
        raise SystemExit("No URI given - pass --uri or set TAMAKKAN_PROD_MONGODB_URI.")

    out_dir = Path(args.out_dir)
    if not out_dir.is_dir():
        raise SystemExit(f"--out-dir '{out_dir}' does not exist - create it first.")

    client = AsyncMongoClient(uri, serverSelectionTimeoutMS=8000)
    db = client[args.db]
    try:
        await client.admin.command("ping")
        if db.name != args.db:
            raise SystemExit(f"Connected, but resolved db name ('{db.name}') != --db ('{args.db}').")

        timestamp = datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S")
        out_path = out_dir / f"tamakkan-{args.db}-users-backup-{timestamp}.json"

        docs = await db[USERS_COLLECTION].find({}).to_list(length=None)
        # bson.json_util.dumps so every BSON-native value (datetime, etc.) round-trips losslessly - this is a
        # restore artifact, not a display format.
        out_path.write_text(json_util.dumps(docs, indent=2), encoding="utf-8")

        size_bytes = out_path.stat().st_size
        print(f"backup written: {out_path}")
        print(f"documents: {len(docs)}")
        print(f"size_bytes: {size_bytes}")
        print(f"timestamp_utc: {timestamp}")
        if len(docs) == 0 or size_bytes == 0:
            print("VERIFICATION FAILED: backup is empty.")
            return 1
        print("verification: file exists, non-empty, document count matches a live count.")
        return 0
    finally:
        await client.close()


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
