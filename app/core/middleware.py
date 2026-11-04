"""Request body-size limiting.

This has to be ASGI middleware rather than a route dependency or a check
inside a handler, because of *when* FastAPI reads the body. In
`fastapi/routing.py` the body is parsed (`await request.form()` /
`await request.body()`) **before** `solve_dependencies()` runs - so
`Depends(require_role(...))` has not executed yet, and an unauthenticated
caller's multi-gigabyte upload is already spooled to disk by the time the 401
would be produced. Starlette's multipart parser caps non-file *fields* at
`max_part_size` but applies no cap at all to *file* parts, so nothing below
this layer bounds the request either.

Two guards, because either alone is insufficient:

* `Content-Length` is checked up front, which rejects an honest oversized
  upload without reading a single byte of it.
* The receive channel is then counted anyway, because `Content-Length` is
  client-supplied (and absent entirely on a `Transfer-Encoding: chunked`
  request). This is what actually bounds a hostile caller.

The overflow is signalled with `HTTPException(413)` rather than a custom
exception on purpose: `fastapi/routing.py` wraps body parsing in
`except Exception -> HTTPException(400, "There was an error parsing the
body")` but re-raises `HTTPException` untouched, so this is the one exception
type that survives that layer with its own status code intact.
"""

from fastapi import HTTPException, status
from fastapi.responses import JSONResponse
from starlette.types import ASGIApp, Message, Receive, Scope, Send

BODY_TOO_LARGE_DETAIL = "Request body is too large."


def _declared_content_length(scope: Scope) -> int:
    for name, value in scope.get("headers", []):
        if name == b"content-length":
            try:
                return int(value)
            except ValueError:
                return 0  # malformed - let the streaming guard handle it
    return 0


class BodySizeLimitMiddleware:
    """Rejects a request whose body exceeds `max_bytes` with a 413."""

    def __init__(self, app: ASGIApp, max_bytes: int) -> None:
        self.app = app
        self.max_bytes = max_bytes

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        if _declared_content_length(scope) > self.max_bytes:
            await self._reject(scope, receive, send)
            return

        received = 0

        async def counting_receive() -> Message:
            nonlocal received
            message = await receive()
            if message["type"] == "http.request":
                received += len(message.get("body", b""))
                if received > self.max_bytes:
                    raise HTTPException(
                        status_code=status.HTTP_413_CONTENT_TOO_LARGE,
                        detail=BODY_TOO_LARGE_DETAIL,
                    )
            return message

        await self.app(scope, counting_receive, send)

    async def _reject(self, scope: Scope, receive: Receive, send: Send) -> None:
        response = JSONResponse(
            status_code=status.HTTP_413_CONTENT_TOO_LARGE,
            content={"detail": BODY_TOO_LARGE_DETAIL},
        )
        await response(scope, receive, send)
