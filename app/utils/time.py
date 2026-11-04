"""A single UTC-now convention used everywhere in this codebase.

Deliberately naive, not `datetime.now(timezone.utc)`: MongoDB/BSON has no
timezone concept, and PyMongo returns naive datetimes (that represent UTC) on
read by default. Mixing naive and tz-aware datetimes for the same logical
field raises `TypeError` the moment you compare them - so every datetime this
app stores or compares is naive-UTC, consistently, on both the write and read
side.
"""

from datetime import datetime, timezone


def utcnow() -> datetime:
    return datetime.now(timezone.utc).replace(tzinfo=None)
