"""One-off data migration for the Phase F multi-course-day change.

`CalendarDayEntry.topic_id: str` became `topic_ids: list[str]`. Documents
written before this change still have the old singular field, which fails
Beanie/Pydantic validation on read (`topic_ids: Field required`) - this
script rewrites every such day entry in place, wrapping the old value in a
single-element list. Bypasses Beanie entirely (raw PyMongo) since Beanie
can't even load a document with the old shape to resave it.

Idempotent - a day entry that already has `topic_ids` (and no `topic_id`) is
left untouched, so running this more than once is harmless.

Usage:
    python -m app.jobs.migrate_calendar_day_topic_ids
"""

import asyncio

from pymongo import AsyncMongoClient

from app.core.config import get_settings


async def _migrate() -> None:
    settings = get_settings()
    client = AsyncMongoClient(settings.MONGODB_URI)
    try:
        collection = client[settings.MONGODB_DB_NAME]["calendar_templates"]
        migrated = 0
        async for doc in collection.find({"days.topic_id": {"$exists": True}}):
            new_days = [
                {"day": entry["day"], "topic_ids": entry["topic_ids"] if "topic_ids" in entry else [entry["topic_id"]]}
                for entry in doc.get("days", [])
            ]
            await collection.update_one({"_id": doc["_id"]}, {"$set": {"days": new_days}})
            migrated += 1
        print(f"Migrated {migrated} calendar template(s).")
    finally:
        await client.close()


if __name__ == "__main__":
    asyncio.run(_migrate())
