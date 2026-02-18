from typing import Annotated

from fastapi import APIRouter, Depends, Request

from server.auth import verify_token
from server.models import QueueStatusResponse
from server.queue import TranscriptionQueue

router = APIRouter()


@router.get("/v1/queue/status")
async def queue_status(
    request: Request,
    token_fingerprint: Annotated[str, Depends(verify_token)],
) -> QueueStatusResponse:
    queue: TranscriptionQueue = request.app.state.queue
    return queue.get_queue_info(token_fingerprint)
