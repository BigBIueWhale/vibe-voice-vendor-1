import json
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from functools import partial
from typing import ClassVar

import httpx
from fastapi import FastAPI
from starlette.types import ASGIApp, Receive, Scope, Send

from server.config import Settings
from server.queue import TranscriptionQueue
from server.routes import health, queue_status, transcribe
from server.transcribe import process_groq_job, process_vibevoice_job

_BACKEND_HTTP_TIMEOUT = httpx.Timeout(connect=10.0, read=600.0, write=60.0, pool=10.0)
_BACKEND_HTTP_LIMITS = httpx.Limits(
    max_connections=8,
    max_keepalive_connections=2,
    keepalive_expiry=10.0,
)


class RequireHTTPSMiddleware:
    """Reject non-HTTPS requests on protected endpoints (checks X-Forwarded-Proto)."""

    _OPEN_PATHS: ClassVar[set[str]] = {"/health"}

    def __init__(self, app: ASGIApp) -> None:
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] == "http" and scope["path"] not in self._OPEN_PATHS:
            headers = dict(scope["headers"])
            proto_bytes = headers.get(b"x-forwarded-proto")
            if proto_bytes is None or proto_bytes.lower() != b"https":
                body = json.dumps({"detail": "HTTPS required"}).encode()
                await send(
                    {
                        "type": "http.response.start",
                        "status": 403,
                        "headers": [
                            [b"content-type", b"application/json"],
                            [b"content-length", str(len(body)).encode()],
                        ],
                    }
                )
                await send({"type": "http.response.body", "body": body})
                return
        await self.app(scope, receive, send)


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    config = app.state.settings

    http_client = httpx.AsyncClient(
        trust_env=False,
        follow_redirects=False,
        http2=False,
        timeout=_BACKEND_HTTP_TIMEOUT,
        limits=_BACKEND_HTTP_LIMITS,
    )
    app.state.http_client = http_client

    queue = TranscriptionQueue(max_size=config.max_queue_size)
    if config.asr_backend == "groq":
        process_fn = partial(process_groq_job, http_client=http_client, config=config)
    else:
        process_fn = partial(process_vibevoice_job, http_client=http_client, config=config)
    queue.set_process_fn(process_fn)
    queue.start_worker()
    app.state.queue = queue

    yield

    await queue.stop()
    await http_client.aclose()


def create_app(settings: Settings) -> FastAPI:
    app = FastAPI(
        title="VibeVoice ASR Server",
        lifespan=lifespan,
        docs_url=None,
        redoc_url=None,
        openapi_url=None,
    )
    app.state.settings = settings

    app.include_router(transcribe.router)
    app.include_router(queue_status.router)
    app.include_router(health.router)

    if settings.require_https:
        app.add_middleware(RequireHTTPSMiddleware)

    return app
