"""Test-only MongoDB: `mongomock-motor` (in-memory, no real server needed).

One compatibility shim is required and lives here, nowhere near production
code: `mongomock` 4.3.0's `Database.list_collection_names()` doesn't accept
the `authorizedCollections` kwarg that Beanie 2.2.0 passes during
`init_beanie()`'s startup collection check. This wraps just that one method
to accept and drop unknown kwargs - confirmed (empirically, not assumed) to
still exercise mongomock's real query engine correctly afterward, including
Link resolution.
"""

import os

# MONGODB_DB_NAME has no default (a deployment must name its database), so the
# test run names its own before the application settings are first loaded.
os.environ.setdefault("MONGODB_DB_NAME", "tamakkan_test")
# A developer's real `.env` (local MongoDB, LiveKit and SpatialReal credentials) must never leak into the tests: they assume
# those services are unconfigured. Point the settings loader at a file that does not exist; real environment variables still apply.
os.environ.setdefault("TAMAKKAN_ENV_FILE", os.path.join(os.path.dirname(__file__), "no-env-file-for-tests"))

import mongomock
import pytest
import pytest_asyncio
from beanie import init_beanie
from fastapi.testclient import TestClient
from mongomock_motor import AsyncMongoMockClient

from app.core.application import app
from app.core.config import Settings
from app.core.database import DOCUMENT_MODELS

# `Settings(...)` built directly (not through get_settings) would otherwise read the developer's real `.env` as well.
Settings.model_config["env_file"] = None

_original_list_collection_names = mongomock.Database.list_collection_names


def _compat_list_collection_names(self, filter=None, session=None, **_ignored_newer_kwargs):
    return _original_list_collection_names(self, filter=filter, session=session)


mongomock.Database.list_collection_names = _compat_list_collection_names


@pytest_asyncio.fixture(autouse=True)
async def mock_db():
    """Fresh, isolated in-memory database for every test - no cross-test
    pollution, no real MongoDB required.
    """
    client = AsyncMongoMockClient()
    await init_beanie(database=client["tamakkan_test"], document_models=DOCUMENT_MODELS)


@pytest.fixture
def client() -> TestClient:
    # Deliberately not `with TestClient(app) as client` - that would trigger
    # the real lifespan (`init_db()` against `MONGODB_URI`). Beanie is already
    # initialized against the mock DB by `mock_db` above; routes that touch
    # Documents work regardless of which `init_beanie()` call configured them.
    return TestClient(app)
