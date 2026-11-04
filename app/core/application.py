"""FastAPI app factory - CORS, lifespan-managed DB connection, exception
handlers, and versioned-router registration all live here, not in `main.py`.

`main.py` only imports `app` from this module and runs it via uvicorn - it's
the container/production entry point, this is where the app is actually built.
"""

from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from loguru import logger

import app.core.logging_config  # noqa: F401 - configures loguru's sink on import, before anything logs
from app.api.api import api_router
from app.core.config import get_settings
from app.core.database import close_db, init_db
from app.core.exceptions import register_exception_handlers
from app.core.middleware import BodySizeLimitMiddleware

settings = get_settings()

API_DESCRIPTION = """
Backend for **Tamakkan**, the AI-driven English-speaking practice platform.

Three roles, strictly hierarchical in who creates whom: **Root** creates
Administrators, an **Administrator** (the Instructor) creates Students. Role
checks are flat, not hierarchical - Root does not inherit an Administrator's
endpoints. See the backend repository's README
for the full account model and design rationale.
"""

# Order here is the order sections appear in Swagger's sidebar - deliberately
# the rough order a new account moves through the API, not alphabetical.
TAGS_METADATA = [
    {"name": "Health", "description": "Liveness check - no auth, no dependencies."},
    {"name": "Auth", "description": "Login, refresh, logout, and self-service password-reset flows."},
    {"name": "Me", "description": "The authenticated caller's own profile and password."},
    {
        "name": "Root",
        "description": "Root-only: create and manage Administrator accounts. "
        "Reachable via Swagger only - there is no dashboard for it.",
    },
    {
        "name": "Students",
        "description": "Administrator-only: create and manage the Students you created. "
        "Never another Administrator's.",
    },
    {
        "name": "Topics",
        "description": "Lesson topics/content. Administrators author them; every "
        "authenticated role can read.",
    },
    {
        "name": "Submissions",
        "description": "Student-submitted audio recordings for a topic, and the views "
        "Students/Administrators use to review them.",
    },
    {
        "name": "Operations",
        "description": "Internal, roster-scoped snapshots of persisted session states. "
        "These are not LiveKit heartbeats or provider billing data.",
    },
]


@asynccontextmanager
async def lifespan(_: FastAPI):
    await init_db()
    if not get_settings().GEMINI_API_KEY.strip():
        logger.warning(
            "GEMINI_API_KEY is empty: AI session reports, transcripts and lesson drafting cannot work. "
            "Sessions will still be recorded, but no report will be generated for them."
        )
    yield
    await close_db()


def create_app() -> FastAPI:
    app = FastAPI(
        title=settings.PROJECT_NAME,
        description=API_DESCRIPTION,
        version="0.1.0",
        openapi_tags=TAGS_METADATA,
        lifespan=lifespan,
        swagger_ui_parameters={
            # Keeps a Bearer token entered via "Authorize" across page
            # reloads - the single highest-value tweak for actually using
            # Swagger to test protected routes, not just read about them.
            "persistAuthorization": True,
            # Collapsed, not hidden (0, not -1) - the schema definitions are
            # useful, they just don't need to dominate the page on load.
            "defaultModelsExpandDepth": 0,
        },
    )

    # Order matters. `add_middleware` inserts at the front of the stack, so the
    # *last* one added is the outermost. CORS goes last deliberately: a 413 from
    # the body-size limit still needs CORS headers, or a browser can't read the
    # error and reports it as an opaque network failure instead.
    app.add_middleware(BodySizeLimitMiddleware, max_bytes=settings.MAX_REQUEST_BODY_BYTES)
    app.add_middleware(
        CORSMiddleware,
        allow_origins=settings.CORS_ORIGINS,
        allow_credentials=True,
        allow_methods=["*"],
        allow_headers=["*"],
    )

    register_exception_handlers(app)
    app.include_router(api_router, prefix=settings.API_PREFIX)

    return app


app = create_app()
