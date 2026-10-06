"""Reject oversized and non-JSON POST bodies before FastAPI reads them.

This is ASGI middleware, not a FastAPI dependency, because FastAPI reads the whole body before any
dependency runs. The body is read here in pieces and refused as soon as it passes the cap, so a huge
upload (with or without a Content-Length) costs at most max_bytes of memory.
"""

import logging
import uuid
from collections.abc import Callable, Collection

from starlette.datastructures import Headers
from starlette.responses import JSONResponse
from starlette.types import ASGIApp, Message, Receive, Scope, Send

log = logging.getLogger(__name__)

TOO_LARGE = "That request is too large."
NOT_JSON = "Please send JSON (Content-Type: application/json)."


class RequestGuard:
    """POSTs to paths must say they're JSON and be at most max_bytes() long."""

    def __init__(self, app: ASGIApp, paths: Collection[str], max_bytes: Callable[[], int]):
        self.app = app
        self.paths = set(paths)
        self.max_bytes = max_bytes  # read per request, so tests can change the setting

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http" or scope["method"] != "POST" or scope["path"] not in self.paths:
            await self.app(scope, receive, send)
            return

        headers = Headers(scope=scope)
        content_type = headers.get("content-type", "").split(";")[0].strip().lower()
        if content_type != "application/json":
            await _reject(scope, receive, send, 415, NOT_JSON, "not_json")
            return
        limit = self.max_bytes()
        length = headers.get("content-length", "")
        if length and (not length.isdigit() or int(length) > limit):
            await _reject(scope, receive, send, 413, TOO_LARGE, "too_large")
            return

        body = b""
        while True:
            message = await receive()
            if message["type"] == "http.disconnect":
                return
            body += message.get("body", b"")
            if len(body) > limit:  # no Content-Length, or one that undercounts
                await _reject(scope, receive, send, 413, TOO_LARGE, "too_large")
                return
            if not message.get("more_body", False):
                break

        replayed = False

        async def replay() -> Message:
            nonlocal replayed
            if replayed:
                return await receive()  # the app waiting on a disconnect
            replayed = True
            return {"type": "http.request", "body": body, "more_body": False}

        await self.app(scope, replay, send)


async def _reject(
    scope: Scope, receive: Receive, send: Send, status: int, detail: str, outcome: str
) -> None:
    log.info(
        "request rejected request_id=%s path=%s outcome=%s",
        uuid.uuid4().hex,
        scope["path"],
        outcome,
    )
    await JSONResponse({"detail": detail}, status_code=status)(scope, receive, send)
