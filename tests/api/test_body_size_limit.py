"""`BodySizeLimitMiddleware` (see `app/core/middleware.py`).

Tested against a purpose-built tiny app rather than the real one, with a tiny
`max_bytes`: the real limit is 30 MB, and pushing that through `TestClient`
for every assertion would be slow for no extra confidence. What matters is the
middleware's contract, and these mirror the real registration order.

The route deliberately carries an always-401 dependency, because the whole
point of this middleware is that FastAPI parses the body *before* dependencies
run - a 413 here proves the body was rejected before auth, which is exactly
the unauthenticated-upload case the middleware exists for.
"""

from typing import Iterator

import pytest
from fastapi import Depends, FastAPI, File, HTTPException, Request, UploadFile
from fastapi.middleware.cors import CORSMiddleware
from fastapi.testclient import TestClient
from pydantic import BaseModel

from app.core.middleware import BodySizeLimitMiddleware

MAX = 1024


def _always_401() -> None:
    raise HTTPException(status_code=401, detail="nope")


class _Body(BaseModel):
    value: str


@pytest.fixture
def client() -> TestClient:
    app = FastAPI()

    @app.post("/upload")
    async def upload(file: UploadFile = File(...), _: None = Depends(_always_401)) -> dict:
        return {"size": len(await file.read())}

    @app.post("/json")
    async def json_route(body: _Body, _: None = Depends(_always_401)) -> dict:
        return {"len": len(body.value)}

    @app.post("/open")
    async def open_route(file: UploadFile = File(...)) -> dict:
        return {"size": len(await file.read())}

    @app.post("/raw")
    async def raw_route(request: Request) -> dict:
        # Reads the receive channel directly - this is what the streaming
        # guard has to protect, and the only shape that can arrive with no
        # `Content-Length` at all.
        return {"size": len(await request.body())}

    # Same order as `create_app`: limit added first, CORS last (outermost).
    app.add_middleware(BodySizeLimitMiddleware, max_bytes=MAX)
    app.add_middleware(CORSMiddleware, allow_origins=["http://localhost:5173"], allow_credentials=True)
    return TestClient(app)


def _chunked(total: int, chunk: int = 256) -> Iterator[bytes]:
    """A generator body makes httpx use `Transfer-Encoding: chunked`, so no
    `Content-Length` header exists and only the streaming guard can catch it.
    """
    sent = 0
    while sent < total:
        step = min(chunk, total - sent)
        yield b"x" * step
        sent += step


def test_oversized_content_length_is_rejected_before_auth_runs(client):
    response = client.post("/upload", files={"file": ("big.m4a", b"x" * (MAX * 4), "audio/m4a")})
    assert response.status_code == 413
    assert response.json() == {"detail": "Request body is too large."}


def test_oversized_chunked_body_with_no_content_length_is_rejected(client):
    """The `Content-Length` pre-check cannot help here - httpx sends this
    `Transfer-Encoding: chunked`, so the header is absent entirely and only the
    receive-channel counter can stop it.
    """
    response = client.post("/raw", content=_chunked(MAX * 4))
    assert "content-length" not in {k.lower() for k in response.request.headers}
    assert response.status_code == 413


def test_a_chunked_body_under_the_limit_is_delivered_intact(client):
    response = client.post("/raw", content=_chunked(300))
    assert response.status_code == 200
    assert response.json() == {"size": 300}


def test_oversized_json_body_is_rejected(client):
    response = client.post("/json", json={"value": "x" * (MAX * 4)})
    assert response.status_code == 413


def test_a_request_under_the_limit_still_reaches_its_dependencies(client):
    """Proves the middleware isn't rejecting everything: this one gets far
    enough to hit the 401, i.e. the body was accepted and auth actually ran.
    """
    response = client.post("/upload", files={"file": ("small.m4a", b"x" * 16, "audio/m4a")})
    assert response.status_code == 401


def test_an_accepted_body_is_delivered_intact(client):
    response = client.post("/open", files={"file": ("small.m4a", b"x" * 300, "audio/m4a")})
    assert response.status_code == 200
    assert response.json() == {"size": 300}


def test_the_413_carries_cors_headers(client):
    """CORS is the outermost middleware precisely so a browser can read this
    error instead of seeing an opaque network failure.
    """
    response = client.post(
        "/upload",
        files={"file": ("big.m4a", b"x" * (MAX * 4), "audio/m4a")},
        headers={"Origin": "http://localhost:5173"},
    )
    assert response.status_code == 413
    assert response.headers["access-control-allow-origin"] == "http://localhost:5173"


def test_a_get_with_no_body_is_untouched(client):
    assert client.get("/nope").status_code == 404
