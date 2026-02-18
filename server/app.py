from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from functools import partial

import httpx
from fastapi import FastAPI

from server.config import Settings
from server.queue import TranscriptionQueue
from server.routes import health, queue_status, transcribe
from server.transcribe import process_transcription_job


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    config = app.state.settings

    http_client = httpx.AsyncClient()
    app.state.http_client = http_client

    queue = TranscriptionQueue(max_size=config.max_queue_size)
    queue.set_process_fn(
        partial(process_transcription_job, http_client=http_client, config=config)
    )
    queue.start_worker()
    app.state.queue = queue

    yield

    await queue.stop()
    await http_client.aclose()


def create_app(settings: Settings | None = None) -> FastAPI:
    if settings is None:
        settings = Settings()

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

    return app
