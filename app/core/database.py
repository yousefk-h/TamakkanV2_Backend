"""MongoDB connection and Beanie initialization.

`init_db()` / `close_db()` are called from the FastAPI app's lifespan
(see `app.main`), not from module import time, so tests can control exactly
when a connection is opened.

Uses PyMongo's native async client (`pymongo.AsyncMongoClient`), not Motor.
Motor 3.7.1 (its latest release) predates PyMongo 4.14's `append_metadata`
method and has no special case for it, so unknown attribute access on a
Motor client/database falls through to its catch-all `__getattr__`, which
returns a same-named `Database`/`Collection` proxy instead of raising
`AttributeError`. Beanie 2.2.0's `init_beanie()` does
`callable(database.client.append_metadata)` to detect whether it can register
driver metadata - that proxy object is technically callable (it defines
`__call__`, only to immediately raise), so Beanie calls it and crashes with
`TypeError: ... object is not callable`. PyMongo's own async client has a
real `append_metadata` method, so it doesn't hit this at all - and it's what
Beanie itself already uses internally for the `connection_string=` path.
"""

from loguru import logger
from pymongo import AsyncMongoClient

from beanie import init_beanie

from app.core.config import get_settings
from app.models.alert_log import AlertLog
from app.models.audit_log import AuditLog
from app.models.calendar_template import CalendarTemplate
from app.models.live_class import LiveClass
from app.models.live_session import LiveSession
from app.models.password_reset_request import PasswordResetRequest
from app.models.password_reset_token import PasswordResetToken
from app.models.platform_avatar_config import PlatformAvatarConfig
from app.models.recording_submission import RecordingSubmission
from app.models.refresh_token import RefreshToken
from app.models.topic_content import TopicContent
from app.models.unit import Unit
from app.models.user import User

_client: AsyncMongoClient | None = None

# Every Beanie Document must be listed here, or it will construct without
# error but silently never persist.
DOCUMENT_MODELS: list = [
    User,
    RefreshToken,
    PasswordResetToken,
    PasswordResetRequest,
    TopicContent,
    Unit,
    LiveSession,
    RecordingSubmission,
    AuditLog,
    AlertLog,
    PlatformAvatarConfig,
    LiveClass,
    CalendarTemplate,
]


async def init_db() -> None:
    global _client
    settings = get_settings()
    _client = AsyncMongoClient(settings.MONGODB_URI)
    await init_beanie(
        database=_client[settings.MONGODB_DB_NAME],
        document_models=DOCUMENT_MODELS,
    )
    # Not the URI - it can carry credentials.
    logger.info("Connected to MongoDB database '{}'", settings.MONGODB_DB_NAME)


async def close_db() -> None:
    global _client
    if _client is not None:
        await _client.close()
        _client = None
        logger.info("MongoDB connection closed")
