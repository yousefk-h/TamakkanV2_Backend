"""Ephemeral, loopback-only API for UI development when local MongoDB is unavailable.

Run with TAMAKKAN_DEV_PASSWORD set. No sessions or metrics are seeded; all data
disappears when this process exits. Never use this runner for production checks.
"""

import os
import secrets
from contextlib import asynccontextmanager

os.environ["TAMAKKAN_ENV_FILE"] = os.path.join(os.path.dirname(__file__), "no-env-file")
os.environ["LOCAL_DEV_MODE"] = "true"
os.environ["ENVIRONMENT"] = "development"
os.environ["MONGODB_URI"] = "mongodb://127.0.0.1:27049"
os.environ["MONGODB_DB_NAME"] = "tamakkan_v2_inmemory"
os.environ["LIVEKIT_URL"] = ""
os.environ["LIVEKIT_AGENT_NAME"] = "tamakkan-v2-local"
os.environ["JWT_SECRET_KEY"] = secrets.token_urlsafe(64)

import mongomock  # noqa: E402
import uvicorn  # noqa: E402
from beanie import init_beanie  # noqa: E402
from mongomock_motor import AsyncMongoMockClient  # noqa: E402

from app.core.application import app  # noqa: E402
from app.core.database import DOCUMENT_MODELS  # noqa: E402
from app.core.security import hash_password  # noqa: E402
from app.models.user import Role, User  # noqa: E402

_original_list_collection_names = mongomock.Database.list_collection_names


def _compat_list_collection_names(self, filter=None, session=None, **_ignored):
    return _original_list_collection_names(self, filter=filter, session=session)


mongomock.Database.list_collection_names = _compat_list_collection_names


@asynccontextmanager
async def in_memory_lifespan(_app):
    client = AsyncMongoMockClient()
    await init_beanie(database=client["tamakkan_v2_inmemory"], document_models=DOCUMENT_MODELS)
    await User(email="local-admin@example.com", full_name="Local Admin", role=Role.ADMINISTRATOR,
               password_hash=hash_password(os.environ["TAMAKKAN_DEV_PASSWORD"])).insert()
    yield


if __name__ == "__main__":
    if not os.environ.get("TAMAKKAN_DEV_PASSWORD"):
        raise SystemExit("Set TAMAKKAN_DEV_PASSWORD to start the isolated in-memory API")
    app.router.lifespan_context = in_memory_lifespan
    uvicorn.run(app, host="127.0.0.1", port=4675)
