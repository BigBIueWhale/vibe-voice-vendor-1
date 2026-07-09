from typing import Annotated

from fastapi import APIRouter, Depends, Request

from server.client_identity import require_client_identity
from server.models import QueueStatusResponse
from server.queue import TranscriptionQueue

router = APIRouter()


@router.get("/v1/queue/status")
async def queue_status(
    request: Request,
    client_identity: Annotated[str, Depends(require_client_identity)],
) -> QueueStatusResponse:
    queue: TranscriptionQueue = request.app.state.queue
    return queue.get_queue_info(client_identity)
